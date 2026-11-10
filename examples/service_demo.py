"""Deterministic P1 service demonstration using a local model protocol fixture."""

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from homebrew_harness import (
    Harness,
    HostConfig,
    ServiceSpec,
    ScheduleSpec,
    RemoteService,
)


def completion(message, finish="stop"):
    return {
        "id": "service-demo",
        "object": "chat.completion",
        "created": 0,
        "model": "fixture",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
    }


def run_demo(directory):
    directory = directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        raise ValueError("The demonstration output directory must be empty.")
    control = directory / "control"
    control.mkdir(mode=0o700)
    workspace = directory / "workspace"
    workspace.mkdir()
    responses = [
        completion(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "write",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {"path": "answer.txt", "content": "service complete\n"}
                            ),
                        },
                    }
                ],
            },
            "tool_calls",
        ),
        completion({"role": "assistant", "content": "Created answer.txt."}),
    ]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size > 1000000:
                self.send_error(413)
                return
            requests.append(json.loads(self.rfile.read(size)))
            body = json.dumps(
                responses.pop(0)
                if responses
                else completion({"role": "assistant", "content": "Done."})
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = directory / "model.env"
    environment.write_text(
        "OPENAI_API_KEY=synthetic\nOPENAI_MODEL=fixture\nOPENAI_BASE_URL="
        + f"http://127.0.0.1:{server.server_port}/v1"
        + "\n"
    )
    environment.chmod(0o600)
    # Keep demonstration model selection independent of exported real connections.
    saved = {
        key: value
        for key, value in os.environ.items()
        if key.startswith(("OPENAI_", "ANTHROPIC_", "DASHSCOPE_"))
    }
    for key in saved:
        os.environ.pop(key, None)
    result = None
    try:
        with Harness(
            workdir=control, config=HostConfig.from_files(env_file=environment)
        ) as host:
            spec = ServiceSpec(
                str(workspace),
                schedules=(ScheduleSpec("marker", 0.1),),
                run_defaults={
                    "total_seconds": 40,
                    "request_seconds": 5,
                    "heartbeat_seconds": 0.1,
                    "heartbeat_timeout": 10,
                    "shutdown_seconds": 3,
                },
            )
            service = host.services.start(spec, request_id="demo")
            prompt = "Write answer.txt containing service complete."
            with RemoteService.from_root(control, control=True) as client:
                work = client.work.submit(prompt, request_id="task-one")
                accepted_id = work.id
            # Disconnecting a client leaves the service and its work running.
            with RemoteService.from_root(control, control=True) as client:
                work = client.work.submit(prompt, request_id="task-one")
                report = work.wait(30)
                if report.snapshot.state != "review":
                    raise RuntimeError(
                        "Service work did not reach its delegated review boundary."
                    )
                run_id = work.snapshot().details["run_id"]
                scheduled = client.schedules()["items"][0]["sequence"] > 0
                client.stop(request_id="stop-demo")
            if not service.wait_closed(10):
                raise RuntimeError("Service resources remain pending.")
            result = {
                "state": "review",
                "work_id": work.id,
                "native_run": run_id,
                "duplicate_submit_same_work": accepted_id == work.id,
                "control_transport": "http",
                "reconnected_client": True,
                "model_calls": len(requests),
                "paid_model_calls": 0,
                "output": (workspace / "answer.txt").read_text(),
                "resources_closed": service.snapshot().cleanup["resources_closed"],
                "scheduled_marker": scheduled,
            }
    finally:
        os.environ.update(saved)
        server.shutdown()
        server.server_close()
        thread.join(2)
    (directory / "result.json").write_text(json.dumps(result, indent=2))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_demo(args.output), indent=2))


if __name__ == "__main__":
    main()
