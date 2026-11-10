"""Local GitHub/model protocol peer for service lifecycle tests."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs


def completion(content=None, tool=None, arguments=None):
    message = {"role": "assistant", "content": content}
    if tool:
        message["tool_calls"] = [
            {
                "id": "proposal",
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


class Peer:
    def __init__(self):
        self.issues = []
        self.comments = []
        self.model_requests = []
        self.source_requests = []
        self.triage = {
            "disposition": "accept",
            "category": "bug",
            "reason": "Reproducible local defect.",
            "plan": "Fix the described behavior and verify its regression.",
            "scope": "code",
            "priority": 10,
        }
        self.source_failure = 0
        self.hold = None
        self.native_hold = None
        self.native_started = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, body, status=200, headers=None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parsed = urlsplit(self.path)
                outer.source_requests.append(self.path)
                if outer.source_failure:
                    self.send({}, outer.source_failure, {"Retry-After": "1"})
                    return
                parts = parsed.path.rsplit("/", 1)
                if "/issues/" in parsed.path and parts[-1].isdecimal():
                    row = next(
                        (r for r in outer.issues if r["number"] == int(parts[-1])), None
                    )
                    self.send(row or {}, 200 if row else 404)
                    return
                rows = (
                    outer.comments
                    if parsed.path.endswith("/comments")
                    else outer.issues
                )
                query = parse_qs(parsed.query)
                size = int(query.get("per_page", ["50"])[0])
                page = int(query.get("page", ["1"])[0])
                self.send(rows[(page - 1) * size : page * size])

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.model_requests.append(body)
                if outer.hold:
                    outer.hold.wait(10)
                messages = body["messages"]
                if messages[-1]["role"] == "tool":
                    self.send(completion("Candidate recorded."))
                    return
                service = any(
                    "persistent maintenance service" in str(m.get("content"))
                    for m in messages
                    if m["role"] == "system"
                )
                if not service:
                    outer.native_started.set()
                    if outer.native_hold:
                        outer.native_hold.wait()
                    self.send(completion("Local work complete."))
                    return
                data = next(
                    json.loads(m["content"])
                    for m in messages
                    if m["role"] == "user"
                    and isinstance(m.get("content"), str)
                    and m["content"].startswith('{"job":')
                )
                kind = data["job"]["kind"]
                result = (
                    outer.triage
                    if kind == "triage"
                    else {
                        "text": "Verified service update."
                        if kind == "report"
                        else "Discuss the next local change; no permission granted."
                    }
                )
                self.send(
                    completion(
                        tool="service_propose",
                        arguments={"proposal": json.dumps(result)},
                    )
                )

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        if self.hold:
            self.hold.set()
        if self.native_hold:
            self.native_hold.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


def issue(
    number=1, body="Fix the documented local defect.", updated="2026-01-01T00:00:00Z"
):
    return {
        "id": number,
        "number": number,
        "title": "Local defect",
        "body": body,
        "state": "open",
        "updated_at": updated,
        "user": {"id": 17},
    }
