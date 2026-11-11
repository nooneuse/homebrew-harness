"""GitHub/model peer with real local Git refs and injectable lost acknowledgments."""

import json
import os
import subprocess
import threading
import tempfile
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
    def run_git(self, *args, cwd=None, env_extra=None):
        return subprocess.check_output(
            [
                self.git,
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@invalid",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=cwd,
            env={**os.environ, **(env_extra or {})},
            stderr=subprocess.DEVNULL,
        )

    def merge_pull(self, number, *, method="merge", actor=17):
        """Simulate the platform's native operation using real Git history."""
        row = self.pulls[number - 1]
        if row.get("merged"):
            return row["merge_commit_sha"]
        repo = "--git-dir=" + str(self.remote)
        head = self.run_git(repo, "rev-parse", row["head"]["ref"]).decode().strip()
        base = self.run_git(repo, "rev-parse", row["base"]["ref"]).decode().strip()
        if method == "rebase":
            with tempfile.TemporaryDirectory() as directory:
                self.run_git("clone", "--no-checkout", str(self.remote), directory)
                self.run_git("checkout", "--detach", base, cwd=directory)
                source = (
                    self.run_git(repo, "rev-list", "--reverse", base + ".." + head)
                    .decode()
                    .split()
                )
                for oid in source:
                    self.run_git(
                        "cherry-pick",
                        oid,
                        cwd=directory,
                        env_extra={"GIT_COMMITTER_DATE": "2026-01-02T00:00:00Z"},
                    )
                result = (
                    self.run_git("rev-parse", "HEAD", cwd=directory).decode().strip()
                )
                self.run_git(repo, "fetch", directory, "HEAD")
        else:
            tree = (
                self.run_git(repo, "merge-tree", "--write-tree", base, head)
                .decode()
                .splitlines()[0]
            )
            parents = ["-p", base] + (["-p", head] if method == "merge" else [])
            result = (
                self.run_git(
                    repo, "commit-tree", tree, *parents, "-m", "Merge reviewed change"
                )
                .decode()
                .strip()
            )
        self.run_git(
            repo, "update-ref", "refs/heads/" + row["base"]["ref"], result, base
        )
        row.update(
            merged=True,
            state="closed",
            merge_commit_sha=result,
            merged_at="2026-01-01T00:00:00Z",
            merged_by={"id": actor},
        )
        row["head"]["sha"] = head
        return result

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
                try:
                    head = self.ref(row["head"]["ref"])
                except subprocess.CalledProcessError:
                    head = row["head"]["sha"]
                row["head"]["sha"] = head
                row = {
                    **row,
                    "head": {
                        **row["head"],
                        "sha": head,
                        "repo": {"full_name": "example/project"},
                    },
                    "base": {
                        **row["base"],
                        "sha": self.ref(row["base"]["ref"]),
                        "repo": {"full_name": "example/project"},
                    },
                    "draft": row.get("draft", False),
                    "mergeable": row.get("mergeable", True),
                    "mergeable_state": row.get("mergeable_state", "clean"),
                }
                row.setdefault("merged", False)
                row.setdefault("state", "open")
                return row

            def do_GET(self):
                uri = urlsplit(self.path)
                if uri.path == "/repos/example/project":
                    self.emit(
                        {
                            "allow_merge_commit": True,
                            "allow_squash_merge": True,
                            "allow_rebase_merge": True,
                        }
                    )
                elif "/rules/branches/" in uri.path:
                    self.emit(getattr(outer, "branch_rules", []))
                elif "/rulesets/" in uri.path:
                    self.emit(outer.rulesets[int(uri.path.rsplit("/", 1)[-1])])
                elif uri.path.endswith("/protection"):
                    self.emit(
                        getattr(
                            outer,
                            "protection",
                            {
                                "required_status_checks": {
                                    "contexts": ["CI"],
                                    "strict": True,
                                },
                                "required_pull_request_reviews": {},
                                "enforce_admins": {"enabled": True},
                                "allow_force_pushes": {"enabled": False},
                            },
                        )
                    )
                elif uri.path.endswith("/pulls"):
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
                    self.emit(getattr(outer, "reviews", []))
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
                    elif kind == "audit":
                        proposal = {
                            "text": "Fixed repository reviewed.",
                            "findings": getattr(outer, "audit_findings", []),
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
                                "content": getattr(outer, "coding_value", None)
                                or ("2\n" if repair else "1\n"),
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

            def do_PUT(self):
                raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if not self.path.endswith("/merge"):
                    self.emit({}, 404)
                    return
                number = int(self.path.split("/pulls/")[1].split("/")[0])
                row = outer.pulls[number - 1]
                if getattr(outer, "merge_hold", None):
                    outer.merge_hold.wait(10)
                if getattr(outer, "merge_failure", None):
                    self.emit({}, outer.merge_failure)
                    return
                current = self.pull(row)
                if current["head"]["sha"] != raw.get("sha"):
                    self.emit({}, 409)
                    return
                repo = "--git-dir=" + str(outer.remote)
                parent = (
                    outer.run_git(
                        repo,
                        "merge-base",
                        current["base"]["sha"],
                        current["head"]["sha"],
                    )
                    .decode()
                    .strip()
                )
                if parent != current["base"]["sha"]:
                    self.emit({}, 405)
                    return
                result = outer.merge_pull(number, method=raw["merge_method"], actor=12)
                outer.writes.append(("merge", raw))
                if outer.drop == "merge":
                    self.close_connection = True
                    return
                self.emit({"merged": True, "sha": result})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
