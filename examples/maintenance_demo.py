"""Offline P2 demonstration: repository input, authority, restart, tests and reports."""

import argparse
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from homebrew_harness import Harness, HostConfig, ServiceSpec, RemoteService
from homebrew_harness.services.launcher import live


def wait_for(function, seconds=30):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = function()
        if value:
            return value
        time.sleep(0.05)
    raise RuntimeError("The demonstration did not reach its expected state.")


def response(content=None, tool=None, arguments=None):
    message = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [
            {
                "id": "step",
                "type": "function",
                "function": {"name": tool, "arguments": json.dumps(arguments)},
            }
        ]
    return {
        "id": "fixture",
        "object": "chat.completion",
        "created": 0,
        "model": "fixture",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if tool else "stop",
            }
        ],
    }


class DemoPeer:
    def __init__(self):
        self.model_calls = 0
        self.source_calls = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.source_calls += 1
                if "/issues/comments" in self.path:
                    self.send([])
                    return
                self.send(
                    [
                        {
                            "id": n,
                            "number": n,
                            "title": title,
                            "body": "Implement the scoped marker and run unit-tests. Preserve existing behavior.",
                            "state": "open",
                            "updated_at": "2026-01-01T00:00:00Z",
                            "user": {"id": 11},
                        }
                        for n, title in (
                            (1, "Fix local marker"),
                            (2, "Discuss a new feature"),
                        )
                    ]
                )

            def do_POST(self):
                outer.model_calls += 1
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                messages = request["messages"]
                is_service = any(
                    "persistent maintenance service" in str(m.get("content"))
                    for m in messages
                    if m["role"] == "system"
                )
                used = [
                    t["function"]["name"]
                    for m in messages
                    if m["role"] == "assistant"
                    for t in m.get("tool_calls", [])
                ]
                if is_service:
                    data = next(
                        json.loads(m["content"])
                        for m in messages
                        if m["role"] == "user"
                        and isinstance(m.get("content"), str)
                        and m["content"].startswith('{"job":')
                    )
                    kind = data["job"]["kind"]
                    identity = data["job"]["target"]
                    if "load_skill" not in used:
                        self.send(
                            response(
                                tool="load_skill",
                                arguments={
                                    "name": {
                                        "triage": "requirement-triage",
                                        "conversation": "maintenance-planning",
                                        "report": "maintenance-reporting",
                                    }[kind]
                                },
                            )
                        )
                        return
                    if "service_read" not in used:
                        self.send(
                            response(
                                tool="service_read",
                                arguments={
                                    "kind": {
                                        "triage": "work",
                                        "conversation": "history",
                                        "report": "reports",
                                    }[kind],
                                    "identity": identity,
                                },
                            )
                        )
                        return
                    if "service_propose" in used:
                        self.send(response("Candidate saved."))
                        return
                    if kind == "triage":
                        feature = "new feature" in data["input"]["prompt"]
                        proposal = {
                            "disposition": "decide" if feature else "accept",
                            "category": "feature" if feature else "bug",
                            "scope": "code",
                            "reason": "The feature needs operator direction."
                            if feature
                            else "A bounded bug fix.",
                            "plan": "Write src/answer.txt and verify it with the fixed unit-tests recipe. Preserve existing behavior.",
                            "priority": 5 if feature else 10,
                        }
                    else:
                        proposal = {
                            "text": "Local work and fixed tests completed; publication is a separate phase."
                            if kind == "report"
                            else "Keep the approved scope and prioritize the bug before the feature."
                        }
                    self.send(
                        response(
                            tool="service_propose",
                            arguments={"proposal": json.dumps(proposal)},
                        )
                    )
                    return
                if "write_file" not in used:
                    self.send(
                        response(
                            tool="write_file",
                            arguments={
                                "path": "src/answer.txt",
                                "content": "P2 maintenance verified\n",
                            },
                        )
                    )
                elif "run_tests" not in used:
                    self.send(
                        response(tool="run_tests", arguments={"name": "unit-tests"})
                    )
                else:
                    self.send(response("Implemented the marker and ran unit-tests."))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.endpoint = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def run_demo(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("Use an empty demonstration directory.")
    control = directory / "control"
    control.mkdir(mode=0o700)
    workspace = directory / "workspace"
    workspace.mkdir()
    (workspace / "tests").mkdir()
    (workspace / "src").mkdir()
    (workspace / "tests/__init__.py").write_text("")
    (workspace / "tests/test_marker.py").write_text(
        'import unittest\nfrom pathlib import Path\nclass Marker(unittest.TestCase):\n    def test_marker(self):\n        self.assertEqual(Path("src/answer.txt").read_text(), "P2 maintenance verified\\n")\n'
    )
    recipe = directory / "tests.json"
    recipe.write_text(
        json.dumps(
            {
                "version": 1,
                "recipes": [
                    {
                        "name": "unit-tests",
                        "builtin": "unittest",
                        "description": "Verify the marker",
                        "roles": ["lead"],
                        "input_globs": ["src/**", "tests/**"],
                        "locked_globs": ["tests/**"],
                        "timeout": 15,
                        "max_runs": 8,
                        "authorization": "allow",
                    }
                ],
            }
        )
    )
    peer = DemoPeer()
    environment = directory / "fixture.env"
    environment.write_text(
        "OPENAI_API_KEY=synthetic\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL="
        + peer.endpoint
        + "/v1\nSOURCE_ENDPOINT="
        + peer.endpoint
        + "\nSOURCE_TOKEN=synthetic\n"
    )
    environment.chmod(0o600)
    saved = {
        k: v
        for k, v in os.environ.items()
        if k.startswith(("OPENAI_", "ANTHROPIC_", "DASHSCOPE_"))
    }
    for k in saved:
        os.environ.pop(k)
    service = None
    try:
        config = HostConfig.from_files(
            env_file=environment,
            test_config=recipe,
            allow_tests=("unit-tests",),
            shutdown_seconds=5,
        )
        with Harness(workdir=control, config=config) as host:
            spec = ServiceSpec(
                str(workspace),
                profile="repository_maintenance",
                sources=(
                    {
                        "name": "project",
                        "provider": "github",
                        "instance": "primary",
                        "project": "example/project",
                        "api_url_env": "SOURCE_ENDPOINT",
                        "token_env": "SOURCE_TOKEN",
                        "poll_seconds": 0.2,
                        "max_pages": 2,
                    },
                ),
                maintenance={
                    "scopes": {
                        "code": {
                            "write_paths": ["src"],
                            "tools": [
                                "read_file",
                                "write_file",
                                "list_tests",
                                "run_tests",
                                "get_test_result",
                            ],
                            "allow_tests": ["unit-tests"],
                        }
                    },
                    "auto_categories": ["bug"],
                    "job_seconds": 30,
                    "report_seconds": 3600,
                },
                run_defaults={
                    "total_seconds": 90,
                    "request_seconds": 5,
                    "episode_model_calls": 6,
                    "shutdown_seconds": 3,
                    "allow_tests": ["unit-tests"],
                },
            )
            service = host.services.start(spec, lifetime="detached")
            with RemoteService.from_root(control, control=True) as remote:
                pending = wait_for(
                    lambda: next(
                        (r for r in remote.requests() if r.kind == "work_authority"),
                        None,
                    )
                )
                wait_for(
                    lambda: next(
                        (
                            w
                            for w in remote.work.list()["items"]
                            if w["state"] == "review"
                        ),
                        None,
                    )
                )
                conversation = remote.converse(
                    "Which work should we prioritize?", request_id="direction"
                )
                wait_for(lambda: remote.conversation(conversation["id"]).get("result"))
                remote.stop(request_id="restart-stop")
            wait_for(lambda: not live(control))
            service = host.services.start(identity=service.id, lifetime="detached")
            with RemoteService.from_root(control, control=True) as remote:
                restored = wait_for(
                    lambda: next(
                        (r for r in remote.requests() if r.ref.id == pending.ref.id),
                        None,
                    )
                )
                remote.decide(restored, "approve", decision_id="feature")
                wait_for(
                    lambda: (
                        len(
                            [
                                w
                                for w in remote.work.list()["items"]
                                if w["state"] == "review"
                            ]
                        )
                        == 2
                    )
                )
                report = remote.report(request_id="summary")
                wait_for(lambda: remote.report(report["id"])["state"] == "ready")
                remote.acknowledge_report(report["id"])
                self_check = remote.records("events")["items"]
                test_count = 0
                for item in remote.work.list()["items"]:
                    verification = (
                        remote.work.get(item["id"]).snapshot().details["verification"]
                    )
                    evidence = json.loads(
                        remote.record("evidence", verification["id"], limit=16000)[
                            "text"
                        ]
                    )
                    if (
                        not evidence["receipts_complete"]
                        or not evidence["test_receipts"]
                    ):
                        raise RuntimeError(
                            "The demonstration lacks complete test evidence."
                        )
                    if any(
                        r["status"] != "passed" or not r["source_stable"]
                        for r in evidence["test_receipts"]
                    ):
                        raise RuntimeError("The recorded fixed tests did not pass.")
                    test_count += len(evidence["test_receipts"])
                result = {
                    "reviewed_work": 2,
                    "events": len(self_check),
                    "source_deduplicated": len(self_check) == 2,
                    "same_pending_request_after_restart": restored.ref.id
                    == pending.ref.id,
                    "conversation_restored": bool(
                        remote.conversation(conversation["id"]).get("result")
                    ),
                    "report_ready": True,
                    "fixed_tests_passed": test_count,
                    "output": (workspace / "src/answer.txt").read_text(),
                    "model_calls": peer.model_calls,
                    "source_calls": peer.source_calls,
                    "paid_model_calls": 0,
                }
                remote.stop(request_id="finish")
            wait_for(lambda: not live(control))
            result["resources_closed"] = service.snapshot().cleanup["resources_closed"]
        (directory / "result.json").write_text(json.dumps(result, indent=2))
        return result
    finally:
        if service and live(control):
            with RemoteService.from_root(control, control=True) as remote:
                remote.stop()
            wait_for(lambda: not live(control), 15)
        os.environ.update(saved)
        peer.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run_demo(parser.parse_args().output), indent=2))


if __name__ == "__main__":
    main()
