"""Multi-round Issue and PR communication using real local HTTP and durable state."""

import argparse
import io
import json
import os
import threading
import time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from homebrew_harness import Harness, HostConfig, ServiceSpec
from communication_fixture import Peer


def wait(check, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        threading.Event().wait(0.02)
    raise RuntimeError("Demonstration did not reach its expected state.")


def run_demo(base):
    base = base.resolve()
    if base.exists() and any(base.iterdir()):
        raise ValueError("Use a new empty demonstration directory.")
    base.mkdir(parents=True, exist_ok=True)
    control, project = base / "control", base / "project"
    control.mkdir(mode=0o700)
    project.mkdir()
    peer = Peer()
    peer.issue("Please add offline session export.")
    env = base / "model.env"
    env.write_text(
        "OPENAI_API_KEY=synthetic-model\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL="
        + peer.url
        + "/v1\nPROJECT_API="
        + peer.url
        + "\nREAD_TOKEN=synthetic\nWRITE_TOKEN=synthetic\nHARNESS_MODEL_TRANSPORT=http\n"
    )
    env.chmod(0o600)
    old = dict(os.environ)
    os.environ.clear()
    host = service = None
    try:
        host = Harness(
            workdir=control,
            config=HostConfig.from_files(env_file=env, shutdown_seconds=3),
        )
        source = {
            "name": "project",
            "provider": "github",
            "instance": "primary",
            "project": "example/project",
            "api_url_env": "PROJECT_API",
            "token_env": "READ_TOKEN",
            "poll_seconds": 0.1,
            "max_pages": 5,
        }
        spec = ServiceSpec(
            str(project),
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
            communication={
                "mode": "local",
                "projects": [{**source, "token_env": "WRITE_TOKEN"}],
                "auto_intents": ["clarify"],
                "poll_seconds": 0.1,
                "debounce_seconds": 0.1,
                "max_debounce_seconds": 0.2,
            },
        )
        peer.proposals = [
            {
                "next_step": "clarify",
                "reason": "Clarify offline operation.",
                "alignment": "Retain local operation.",
                "questions": [
                    {
                        "id": "offline",
                        "text": "Should export work without network?",
                        "sources": [],
                    }
                ],
                "communication": {
                    "kind": "post_comment",
                    "intent": "clarify",
                    "body": "Should export work without network?",
                },
            }
        ]
        service = host.services.start(spec)
        wait(
            lambda: (
                service.discussions()["items"]
                and service.discussions()["items"][0]["id"]
            )
        )
        wait(lambda: len(peer.writes) == 1)
        peer.proposals = [
            {
                "next_step": "clarify",
                "reason": "Clarify compatibility.",
                "alignment": "Preserve existing formats.",
                "facts": [
                    {
                        "id": "offline",
                        "text": "Offline operation is required.",
                        "sources": [],
                    }
                ],
                "questions": [
                    {
                        "id": "compatibility",
                        "text": "Should existing files remain readable?",
                        "sources": [],
                    }
                ],
                "communication": {
                    "kind": "post_comment",
                    "intent": "clarify",
                    "body": "Should existing files remain readable?",
                },
            }
        ]
        peer.answer("Export must work offline.")
        wait(lambda: len(peer.writes) == 2)
        peer.proposals = [
            {
                "next_step": "develop",
                "reason": "The scoped request is now clear.",
                "alignment": "Fits local operation and compatibility.",
                "draft": {
                    "text": "Add offline export of saved sessions.",
                    "scope": "code",
                    "acceptance": "Existing saved files remain readable.",
                },
            }
        ]
        peer.answer("Existing saved files must remain readable.")
        decision = wait(
            lambda: next(
                (r for r in service.requests() if r.kind == "discussion_requirements"),
                None,
            )
        )
        service.stop()
        assert service.wait_closed(8)
        service = host.services.start(identity=service.id)
        restored = next(r for r in service.requests() if r.ref.id == decision.ref.id)
        assert restored.ref == decision.ref
        service.decide(restored, "approve", decision_id="accept-requirement")
        wait(lambda: any(r.kind == "work_authority" for r in service.requests()))
        assert not service.runtime.child.run_store.runtime.items("runs")
        peer.issues.append(
            {
                "number": 2,
                "id": 2,
                "title": "Preserve save behavior",
                "body": "Review this implementation.",
                "state": "open",
                "updated_at": "2026-11-11T00:01:01Z",
                "user": {"id": 11},
                "pull_request": {},
            }
        )
        peer.proposals = [
            {
                "next_step": "wait",
                "reason": "Review this fixed PR.",
                "alignment": "Keep the documented save behavior.",
                "findings": [
                    {
                        "id": "behavior",
                        "text": "Explain the changed return value.",
                        "sources": [],
                        "status": "needs_explanation",
                    }
                ],
                "communication": {
                    "kind": "submit_review",
                    "intent": "review_feedback",
                    "event": "COMMENT",
                    "body": "Please explain this behavior change.",
                },
            }
        ]
        pr = wait(
            lambda: next(
                (
                    r["id"]
                    for r in service.discussions()["items"]
                    if r["subject"]["kind"] == "change_request"
                ),
                None,
            )
        )
        request = wait(
            lambda: next(
                (r for r in service.requests() if r.kind == "communication_authority"),
                None,
            )
        )
        snapshot = service.discussion(pr)["review_id"]
        service.decide(request, "approve", decision_id="review-1")
        wait(lambda: len(peer.reviews) == 1)
        peer.proposals = [
            {
                "next_step": "wait",
                "reason": "The author explanation is supported.",
                "alignment": "Retains the documented contract.",
                "findings": [
                    {
                        "id": "behavior",
                        "text": "The explanation addresses the concern.",
                        "sources": [],
                        "status": "withdrawn",
                    }
                ],
            }
        ]
        peer.answer("This preserves the documented contract.", number=2)
        wait(
            lambda: (
                service.discussion(pr)["projection"]["findings"][0]["status"]
                == "withdrawn"
            )
        )
        assert service.discussion(pr)["review_id"] == snapshot
        peer.proposals = [
            {
                "next_step": "wait",
                "reason": "A new version has been assessed.",
                "alignment": "Within the project scope.",
            }
        ]
        peer.head = "4" * 40
        peer.files["src/value.py"] = "def save(value):\n    return str(value)\n"
        wait(lambda: service.discussion(pr)["review_id"] not in {None, snapshot})
        wait(lambda: service.discussion(pr)["state"] == "waiting_participant")
        service.stop()
        assert service.wait_closed(8)
        return {
            "status": "completed",
            "issue_public_replies": 2,
            "requirement_rounds": 3,
            "discussion_decision_restored": True,
            "development_not_started_before_authority": True,
            "pr_review_published": len(peer.reviews),
            "same_head_snapshot_reused": True,
            "new_head_snapshot_created": True,
            "paid_model_calls": 0,
            "resources_closed": True,
            "model_http_calls": len(peer.models),
        }
    finally:
        if service and not service.wait_closed(0):
            service.stop()
            service.wait_closed(8)
        if host:
            host.close()
        peer.close()
        os.environ.clear()
        os.environ.update(old)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        result = run_demo(args.output)
    print(json.dumps(result, indent=2))
