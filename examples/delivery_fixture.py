"""GitHub/model peer with real local Git refs and injectable lost acknowledgments."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


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
    def __init__(self, git, remote, *, ci_failure=False):
        self.git, self.remote = git, remote
        self.pulls = []
        self.comments = []
        self.model_requests = []
        self.writes = []
        self.drop = None
        self.ci_failure = ci_failure
        self.failed_head = None
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def emit(self, value, status=200):
                raw = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def ref(self, branch):
                return (
                    subprocess.check_output(
                        [
                            outer.git,
                            "--git-dir=" + str(outer.remote),
                            "rev-parse",
                            "refs/heads/" + branch,
                        ],
                        stderr=subprocess.DEVNULL,
                    )
                    .decode()
                    .strip()
                )

            def pull(self, row):
                row = {
                    **row,
                    "head": {**row["head"], "sha": self.ref(row["head"]["ref"])},
                    "base": {**row["base"], "sha": self.ref(row["base"]["ref"])},
                }
                row["merged"] = row["head"]["sha"] == row["base"]["sha"]
                row["state"] = "closed" if row["merged"] else "open"
                return row

            def do_GET(self):
                uri = urlsplit(self.path)
                if uri.path.endswith("/pulls"):
                    self.emit([self.pull(row) for row in outer.pulls])
                elif "/pulls/" in uri.path and uri.path.rsplit("/", 1)[-1].isdigit():
                    self.emit(
                        self.pull(outer.pulls[int(uri.path.rsplit("/", 1)[-1]) - 1])
                    )
                elif uri.path.endswith("/check-runs"):
                    head = uri.path.split("/commits/")[1].split("/")[0]
                    if outer.ci_failure and outer.failed_head is None:
                        outer.failed_head = head
                    failed = outer.ci_failure and head == outer.failed_head
                    self.emit(
                        {
                            "check_runs": [
                                {
                                    "id": 11,
                                    "name": "CI",
                                    "app": {"id": 11},
                                    "head_sha": getattr(outer, "ci_head", None) or head,
                                    "status": "completed",
                                    "conclusion": "failure" if failed else "success",
                                    "output": {
                                        "summary": "Remote regression requires a repair."
                                        if failed
                                        else "Passed"
                                    },
                                }
                            ]
                        }
                    )
                elif uri.path.endswith("/reviews"):
                    self.emit([])
                elif uri.path.endswith("/comments"):
                    self.emit(outer.comments)
                else:
                    self.emit({}, 404)

            def do_POST(self):
                raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/pulls"):
                    outer.writes.append(("pull", raw))
                    row = {
                        "number": len(outer.pulls) + 1,
                        "title": raw["title"],
                        "body": raw["body"],
                        "head": {"ref": raw["head"]},
                        "base": {"ref": raw["base"]},
                    }
                    outer.pulls.append(row)
                    if outer.drop == "pull":
                        self.close_connection = True
                        return
                    self.emit(self.pull(row))
                    return
                if self.path.endswith("/comments"):
                    outer.writes.append(("comment", raw))
                    row = {"id": len(outer.comments) + 1, "body": raw["body"]}
                    outer.comments.append(row)
                    if outer.drop == "comment":
                        self.close_connection = True
                        return
                    self.emit(row)
                    return
                outer.model_requests.append(raw)
                messages = raw["messages"]
                service = any(
                    "persistent maintenance service" in str(m.get("content"))
                    for m in messages
                    if m["role"] == "system"
                )
                if service:
                    if messages[-1]["role"] == "tool":
                        self.emit(completion("Recorded."))
                        return
                    data = next(
                        json.loads(m["content"])
                        for m in messages
                        if m["role"] == "user"
                        and isinstance(m.get("content"), str)
                        and m["content"].startswith('{"job":')
                    )
                    kind = data["job"]["kind"]
                    if kind == "triage":
                        proposal = {
                            "disposition": "accept",
                            "category": "bug",
                            "scope": "code",
                            "reason": "Bounded defect.",
                            "plan": "Update src/value.txt and verify unit-tests.",
                            "priority": 10,
                        }
                    elif kind == "review":
                        proposal = {"text": "Fixed candidate reviewed.", "findings": []}
                    elif kind == "ci_triage":
                        proposal = {
                            "classification": "code",
                            "reason": "Confirmed current-candidate regression.",
                            "plan": "Set src/value.txt to 2 and preserve the fixed tests.",
                        }
                    else:
                        proposal = {"text": "Delivery status recorded."}
                    self.emit(
                        completion(
                            tool="service_propose",
                            arguments={"proposal": json.dumps(proposal)},
                        )
                    )
                    return
                calls = [
                    t["function"]["name"]
                    for m in messages
                    if m["role"] == "assistant"
                    for t in m.get("tool_calls", [])
                ]
                if "write_file" not in calls:
                    repair = any(
                        "Confirmed CI repair plan" in str(m.get("content"))
                        for m in messages
                    )
                    self.emit(
                        completion(
                            tool="write_file",
                            arguments={
                                "path": "src/value.txt",
                                "content": "2\n" if repair else "1\n",
                            },
                        )
                    )
                else:
                    self.emit(completion("Scoped implementation complete."))

            def do_PATCH(self):
                raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if "/pulls/" in self.path:
                    row = next(
                        r
                        for r in outer.pulls
                        if r["number"] == int(self.path.rsplit("/", 1)[-1])
                    )
                    row.update(raw)
                    outer.writes.append(("pull-update", raw))
                    self.emit(self.pull(row))
                else:
                    self.emit({}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
