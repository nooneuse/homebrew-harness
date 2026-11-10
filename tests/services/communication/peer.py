"""Actual HTTP model/platform peer; no vendor keys or remote publication."""

import base64
import difflib
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, urlunsplit

from tests.support.clients import reply, call


def blob(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


class Peer:
    def __init__(self):
        self.issues = []
        self.comments = []
        self.reviews = []
        self.inline = []
        self.writes = []
        self.models = []
        self.proposals = []
        self.hold = None
        self.drop = None
        self.count = 10
        self.head = "1" * 40
        self.base = "2" * 40
        self.tree = "3" * 40
        self.files = {"src/value.py": "def save(value):\n    return value\n"}
        self.base_files = {"src/value.py": "def save(value):\n    return None\n"}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def send(self, data, status=200):
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                parsed = urlsplit(self.path)
                path = parsed.path
                parts = path.split("/")
                query = parse_qs(parsed.query)
                if path == "/user":
                    return self.send({"id": 7, "login": "fixture-publisher"})
                prefix = "/repos/example/project"
                if path == prefix + "/issues":
                    return self.send(
                        outer.issues if query.get("page", ["1"]) == ["1"] else []
                    )
                if path == prefix + "/issues/comments":
                    return self.send(
                        outer.comments if query.get("page", ["1"]) == ["1"] else []
                    )
                if path.endswith("/comments") and "/issues/" in path:
                    return self.send(
                        [c for c in outer.comments if c["number"] == int(parts[-2])]
                    )
                if path.endswith("/reviews"):
                    return self.send(outer.reviews)
                if "/pulls/comments/" in path:
                    row = next(
                        (r for r in outer.inline if str(r["id"]) == parts[-1]), None
                    )
                    return self.send(row or {}, 200 if row else 404)
                if path.endswith("/comments") and "/pulls/" in path:
                    return self.send(outer.inline)
                if path.endswith("/files"):
                    changed = []
                    for name in set(outer.files) | set(outer.base_files):
                        old, new = (
                            outer.base_files.get(name, ""),
                            outer.files.get(name, ""),
                        )
                        if old != new:
                            changed.append(
                                {
                                    "filename": name,
                                    "status": "modified",
                                    "patch": "".join(
                                        difflib.unified_diff(
                                            old.splitlines(True), new.splitlines(True)
                                        )
                                    )[
                                        "".join(
                                            difflib.unified_diff(
                                                old.splitlines(True),
                                                new.splitlines(True),
                                            )
                                        ).find("@@") :
                                    ],
                                }
                            )
                    return self.send(changed)
                if "/compare/" in path:
                    return self.send({"merge_base_commit": {"sha": outer.base}})
                if "/git/commits/" in path:
                    return self.send({"sha": parts[-1], "tree": {"sha": outer.tree}})
                if "/git/trees/" in path:
                    return self.send(
                        {
                            "sha": outer.tree,
                            "truncated": False,
                            "tree": [
                                {
                                    "path": name,
                                    "mode": "100644",
                                    "type": "blob",
                                    "size": len(text.encode()),
                                    "sha": blob(text.encode()),
                                }
                                for name, text in outer.files.items()
                            ],
                        }
                    )
                if "/git/blobs/" in path:
                    for text in list(outer.files.values()) + list(
                        outer.base_files.values()
                    ):
                        raw = text.encode()
                        if blob(raw) == parts[-1]:
                            return self.send(
                                {
                                    "sha": blob(raw),
                                    "encoding": "base64",
                                    "content": base64.b64encode(raw).decode(),
                                }
                            )
                    return self.send({}, 404)
                if "/contents/" in path:
                    name = path.split("/contents/", 1)[1]
                    text = (
                        outer.base_files.get(name)
                        if query.get("ref") == [outer.base]
                        else outer.files.get(name)
                    )
                    if text is None:
                        return self.send({}, 404)
                    raw = text.encode()
                    return self.send(
                        {
                            "sha": blob(raw),
                            "encoding": "base64",
                            "content": base64.b64encode(raw).decode(),
                        }
                    )
                if "/issues/" in path or "/pulls/" in path:
                    row = next(
                        (r for r in outer.issues if str(r["number"]) == parts[-1]), None
                    )
                    if row is None:
                        return self.send({}, 404)
                    if "/pulls/" in path:
                        row = {
                            **row,
                            "head": {
                                "sha": outer.head,
                                "repo": {"full_name": "example/project"},
                            },
                            "base": {
                                "sha": outer.base,
                                "repo": {"full_name": "example/project"},
                            },
                        }
                    return self.send(row)
                self.send({}, 404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path.endswith("/chat/completions"):
                    outer.models.append(body)
                    if body["messages"][-1].get("role") == "tool":
                        return self.send(
                            reply("Candidate saved.").model_dump(exclude_none=True)
                        )
                    if outer.hold:
                        outer.hold.wait(10)
                    candidate = (
                        outer.proposals.pop(0)
                        if outer.proposals
                        else {
                            "next_step": "wait",
                            "reason": "No pending question.",
                            "alignment": "Matches the current project goals.",
                        }
                    )
                    if callable(candidate):
                        candidate = candidate(body)
                    return self.send(
                        reply(
                            calls=[
                                call(
                                    "proposal-" + str(len(outer.models)),
                                    name="service_propose",
                                    arguments=json.dumps(
                                        {"proposal": json.dumps(candidate)}
                                    ),
                                )
                            ]
                        ).model_dump(exclude_none=True)
                    )
                outer.count += 1
                row = {
                    "id": outer.count,
                    "body": body["body"],
                    "user": {"id": 7},
                    "updated_at": f"2026-11-11T00:00:{outer.count:02d}Z",
                }
                if self.path.endswith("/reviews"):
                    row.update(
                        commit_id=body["commit_id"],
                        state={
                            "COMMENT": "COMMENTED",
                            "APPROVE": "APPROVED",
                            "REQUEST_CHANGES": "CHANGES_REQUESTED",
                        }[body["event"]],
                    )
                    outer.reviews.append(row)
                    for comment in body.get("comments", []):
                        outer.count += 1
                        outer.inline.append(
                            {
                                **row,
                                **comment,
                                "id": outer.count,
                                "pull_request_review_id": row["id"],
                                "pull_request_url": outer.url
                                + "/repos/example/project/pulls/1",
                            }
                        )
                    kind = "review"
                elif self.path.endswith("/replies"):
                    root = self.path.split("/")[-2]
                    row.update(
                        in_reply_to_id=int(root),
                        pull_request_url=outer.url + "/repos/example/project/pulls/1",
                        commit_id=outer.head,
                    )
                    outer.inline.append(row)
                    kind = "reply"
                else:
                    number = int(self.path.split("/")[-2])
                    row.update(
                        number=number,
                        issue_url=outer.url
                        + "/repos/example/project/issues/"
                        + str(number),
                    )
                    outer.comments.append(row)
                    kind = "comment"
                outer.writes.append((kind, row))
                if outer.drop == kind:
                    self.close_connection = True
                    return
                self.send(row)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = urlunsplit(
            ("http", "127.0.0.1:" + str(self.server.server_port), "", "", "")
        )

    def issue(self, body="Please add a feature.", pr=False):
        row = {
            "number": 1,
            "id": 1,
            "title": "Feature",
            "body": body,
            "state": "open",
            "updated_at": "2026-11-11T00:00:01Z",
            "user": {"id": 11},
        }
        if pr:
            row["pull_request"] = {}
        self.issues = [row]

    def answer(self, body, actor=11):
        self.count += 1
        self.comments.append(
            {
                "id": self.count,
                "number": 1,
                "body": body,
                "user": {"id": actor},
                "updated_at": f"2026-11-11T00:00:{self.count:02d}Z",
                "issue_url": self.url + "/repos/example/project/issues/1",
            }
        )

    def close(self):
        if self.hold:
            self.hold.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
